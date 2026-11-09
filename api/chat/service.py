"""Chat service (CT-15). Phase 01: one stateless LLM call per message (TASK-019).

`handle_message` validates the message eagerly, when it is called and before it returns the
event stream, so a blank or too long message (CHAT-90, CHAT-91) raises `AtlasError` before any
LLM call and before an HTTP or SSE response starts. The returned async iterator then makes a
single `llm.complete` call, with the system prompt and no history, and yields one `DoneEvent`.
A provider failure becomes `AtlasError(LLM_UNAVAILABLE, 502)` without the provider text
(CHAT-92). Nothing is persisted in this phase.
"""

from collections.abc import AsyncIterator
from functools import lru_cache

from api.agent.events import AgentEvent, DoneEvent, ErrorEvent
from api.auth.deps import CurrentCustomer
from api.chat.schemas import ChatRequest, ChatResponse
from api.config import Settings, get_settings
from api.errors import AtlasError, ErrorCode
from api.llm.client import ChatMessage, LLMClient, LLMError, LLMRequest, SystemBlock
from api.llm.factory import build_llm_client
from api.messages import Lang
from api.observability.logging import get_trace_id, log_event
from api.prompts import load_prompt

DEFAULT_LANGUAGE: Lang = "en"


class ChatService:
    def __init__(self, llm: LLMClient, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings

    def handle_message(
        self, request: ChatRequest, principal: CurrentCustomer | None
    ) -> AsyncIterator[AgentEvent]:
        """Validate `request` now (raises `AtlasError`), then return its event stream."""
        self._validate(request.message)
        return self._answer(request)

    async def collect(self, events: AsyncIterator[AgentEvent]) -> ChatResponse:
        """Fold an event stream into the JSON response of `POST /chat`."""
        async for event in events:
            if isinstance(event, ErrorEvent):
                raise _error_from_code(event.code)
            if isinstance(event, DoneEvent):
                return ChatResponse(
                    conversation_id=event.conversation_id,
                    message_id=event.message_id,
                    status=event.status,
                    content=event.content,
                    language=event.language,
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

    async def _answer(self, request: ChatRequest) -> AsyncIterator[AgentEvent]:
        language: Lang = request.ui_language or DEFAULT_LANGUAGE
        llm_request = LLMRequest(
            system=[SystemBlock(load_prompt("system"))],
            messages=[ChatMessage("user", request.message)],
            purpose="chat",
        )
        try:
            response = await self.llm.complete(llm_request)
        except LLMError as exc:
            log_event(
                "chat_llm_unavailable",
                exc_type=type(exc).__name__,
                recoverable=exc.recoverable,
                status=exc.status,
            )
            raise AtlasError(ErrorCode.LLM_UNAVAILABLE, 502) from exc
        yield DoneEvent(
            status="completed",
            content=response.text,
            language=language,
            trace_id=get_trace_id(),
            conversation_id=None,
            message_id=None,
        )


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


def get_chat_service() -> ChatService:
    """FastAPI dependency; tests override it with `app.dependency_overrides`."""
    return ChatService(_llm_client(), get_settings())
