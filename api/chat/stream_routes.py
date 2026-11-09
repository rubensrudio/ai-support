"""Streaming chat endpoints (CT-76, STR-01, STR-04, STR-05, STR-90, STR-91).

`POST /chat/stream` (body CT-14 `ChatRequest`) and `POST /chat/confirmations/{id}/stream` (body
CT-46) -> 200 `text/event-stream` (`Cache-Control: no-cache`, `X-Accel-Buffering: no`) with the
CT-45 events encoded by `api.streaming.events`; 401 before opening the stream. The JSON
`POST /chat` stays (demos and evals use it).

Before the stream opens (JSON error envelope, not SSE):
- `require_customer` runs as a dependency: no valid session -> 401 `AUTH_REQUIRED`, and the
  service is never called;
- an invalid body is 422 `VALIDATION_ERROR` (FastAPI validation), and a blank or too long
  message is 422 too, because `ChatService.handle_message` validates eagerly.

Inside the stream (`sse_stream`):
- the events go through `guard_terminal` (one terminal event, always last) and `encode_sse`;
- before each event is pulled, `await request.is_disconnected()` is checked; on a disconnect the
  stream stops and closes the service stream, which stores the partial answer as `cancelled`
  and commits the tools that already ran (STR-91, STR-07). When Starlette itself cancels the
  response on a disconnect, the cancellation reaches the service the same way;
- an exception (`AtlasError`, e.g. 404/409 of a confirmation, `LLMError`, or anything
  unexpected) becomes one `event: error` with the catalogue code, the user message in the
  Customer's language and the `trace_id`, and the stream ends: no `done` after it (STR-90). The
  request's work since its last commit is rolled back first (the user message is already
  committed, CHAT-92): the request session would otherwise commit at teardown, since the
  exception does not leave the endpoint. Unexpected exceptions log only their type;
- a service stream that ends without a terminal event also ends with an `INTERNAL_ERROR` event.
"""

from collections.abc import AsyncIterator
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from api.agent.events import AgentEvent, ErrorEvent
from api.agent.loop import closing_stream
from api.auth.deps import CurrentCustomer, require_customer
from api.chat.routes import ConfirmationDecision
from api.chat.schemas import ChatRequest
from api.chat.service import ChatService, get_chat_service
from api.errors import AtlasError, ErrorCode, error_message
from api.llm.client import LLMError
from api.messages import Lang
from api.observability.logging import get_trace_id, log_event
from api.streaming.events import encode_sse, guard_terminal

stream_router = APIRouter(tags=["chat"])

SSE_MEDIA_TYPE = "text/event-stream"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
_TERMINAL_TYPES = frozenset({"done", "error"})


class DisconnectAware(Protocol):
    """What `sse_stream` needs from the request (Starlette's `Request` has it)."""

    async def is_disconnected(self) -> bool: ...


@stream_router.post("/chat/stream", response_class=StreamingResponse)
async def post_chat_stream(
    request: Request,
    body: ChatRequest,
    customer: Annotated[CurrentCustomer, Depends(require_customer)],
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> StreamingResponse:
    # Validates the message now: a 422 is raised before the stream opens.
    events = service.handle_message(body, customer)
    return _sse_response(sse_stream(request, events, service=service, language=customer.language))


@stream_router.post(
    "/chat/confirmations/{confirmation_id}/stream", response_class=StreamingResponse
)
async def post_confirmation_stream(
    request: Request,
    confirmation_id: UUID,
    body: ConfirmationDecision,
    customer: Annotated[CurrentCustomer, Depends(require_customer)],
    service: Annotated[ChatService, Depends(get_chat_service)],
) -> StreamingResponse:
    events = service.resolve_confirmation(confirmation_id, body.decision, customer)
    return _sse_response(sse_stream(request, events, service=service, language=customer.language))


def _sse_response(content: AsyncIterator[bytes]) -> StreamingResponse:
    return StreamingResponse(content, media_type=SSE_MEDIA_TYPE, headers=dict(SSE_HEADERS))


async def sse_stream(
    request: DisconnectAware,
    events: AsyncIterator[AgentEvent],
    *,
    service: ChatService,
    language: Lang,
) -> AsyncIterator[bytes]:
    """Encode `events` as SSE, ending with exactly one terminal event (see the module doc)."""
    error: ErrorEvent
    try:
        async with closing_stream(guard_terminal(events)) as guarded:
            while True:
                # Checked before pulling the next event, so the stored partial is exactly
                # what the client received.
                if await request.is_disconnected():
                    log_event("chat_stream_client_disconnected")
                    return  # closing the stream stores the partial answer (STR-91)
                try:
                    event = await anext(guarded)
                except StopAsyncIteration:
                    break
                yield encode_sse(event)
                if event.type in _TERMINAL_TYPES:
                    return
    except AtlasError as exc:
        log_event("chat_stream_error", code=exc.code.value)
        error = _error_event(exc.code, language, exc.params)
    except LLMError as exc:
        log_event(
            "chat_llm_unavailable",
            exc_type=type(exc).__name__,
            recoverable=exc.recoverable,
            status=exc.status,
        )
        error = _error_event(ErrorCode.LLM_UNAVAILABLE, language)
    except Exception as exc:
        log_event("chat_stream_failed", exc_type=type(exc).__name__)
        error = _error_event(ErrorCode.INTERNAL_ERROR, language)
    else:
        log_event("chat_stream_without_terminal_event")
        error = _error_event(ErrorCode.INTERNAL_ERROR, language)
    await _rollback(service)
    yield encode_sse(error)


async def _rollback(service: ChatService) -> None:
    try:
        await service.rollback()
    except Exception as exc:
        log_event("chat_stream_rollback_failed", exc_type=type(exc).__name__)


def _error_event(
    code: ErrorCode, language: Lang, params: dict[str, object] | None = None
) -> ErrorEvent:
    return ErrorEvent(
        code=code.value,
        message=error_message(code, language, **(params or {})),
        trace_id=get_trace_id(),
    )
