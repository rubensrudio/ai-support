"""Anthropic adapter (CT-6 over the `anthropic` SDK).

This is the only module that imports the Anthropic SDK. It converts `LLMRequest` into a Messages
API call and normalizes the response, token usage and errors. The SDK runs with `max_retries=0`:
retries belong to our resilience chain (DA-14). Structured output uses the SDK's native
`output_config.format` (`json_schema`), so the JSON comes back in `LLMResponse.text`.
The SDK (2.x) runs on `httpx2`, so an injected HTTP client must be an `httpx2.AsyncClient`.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx2
from anthropic import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    omit,
)
from anthropic.types import (
    Message,
    MessageParam,
    OutputConfigParam,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStreamEvent,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)

from api.config import MissingConfigError, Settings
from api.llm.client import (
    ChatMessage,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMTimeoutError,
    SystemBlock,
    ToolCall,
    ToolSpec,
    Usage,
)

PROVIDER = "anthropic"
RECOVERABLE_STATUSES = frozenset({408, 409, 429})
# An `error` event inside a 200 stream carries no HTTP status; map its error type to the status
# the API uses for it, so `recoverable` follows the same rule as a failed request.
STREAM_ERROR_STATUSES = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "api_error": 500,
    "timeout_error": 504,
    "overloaded_error": 529,
}


def is_recoverable_status(status: int) -> bool:
    """429, 408/409 and every 5xx (including Anthropic's 529 overloaded) are worth retrying."""
    return status in RECOVERABLE_STATUSES or status >= 500


def _system_blocks(system: list[SystemBlock]) -> list[TextBlockParam]:
    # cache_breakpoint is ignored until phase 11 (prompt cache).
    return [{"type": "text", "text": block.text} for block in system if block.text]


def _tool_params(tools: list[ToolSpec]) -> list[ToolParam]:
    return [
        {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
        for tool in tools
    ]


def _assistant_message(message: ChatMessage) -> MessageParam:
    if not message.tool_calls:
        return {"role": "assistant", "content": message.content}
    blocks: list[TextBlockParam | ToolUseBlockParam] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    blocks.extend(
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        for call in message.tool_calls
    )
    return {"role": "assistant", "content": blocks}


def _messages(messages: list[ChatMessage]) -> list[MessageParam]:
    """Map our messages to Messages API turns.

    Consecutive `tool` messages answer one assistant turn, so they are merged into a single
    `user` turn of `tool_result` blocks, as the API requires.
    """
    converted: list[MessageParam] = []
    pending_results: list[ToolResultBlockParam] = []

    def flush_results() -> None:
        if pending_results:
            converted.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in messages:
        if message.role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content,
                }
            )
            continue
        flush_results()
        if message.role == "assistant":
            converted.append(_assistant_message(message))
        else:
            converted.append({"role": "user", "content": message.content})
    flush_results()
    return converted


def _require[T](value: object, kind: type[T], field: str) -> T:
    """The SDK builds response models without validation; reject fields of the wrong type."""
    if not isinstance(value, kind) or isinstance(value, bool) and kind is int:
        raise ValueError(f"malformed response field: {field}")
    return value


def _to_response(message: Message) -> LLMResponse:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in _require(message.content, list, "content"):
        if block.type == "text":
            texts.append(_require(block.text, str, "text"))
        elif block.type == "tool_use":
            calls.append(
                ToolCall(
                    id=_require(block.id, str, "tool_use.id"),
                    name=_require(block.name, str, "tool_use.name"),
                    arguments=dict(_require(block.input, dict, "tool_use.input")),
                )
            )
    usage = message.usage
    return LLMResponse(
        text="".join(texts),
        tool_calls=calls,
        usage=Usage(
            input_tokens=_require(usage.input_tokens, int, "usage.input_tokens"),
            output_tokens=_require(usage.output_tokens, int, "usage.output_tokens"),
            cache_read_tokens=_require(usage.cache_read_input_tokens or 0, int, "usage"),
            cache_write_tokens=_require(usage.cache_creation_input_tokens or 0, int, "usage"),
        ),
        model=_require(message.model, str, "model"),
        provider=PROVIDER,
        stop_reason=message.stop_reason,
    )


def _malformed_response(exc: Exception) -> LLMError:
    return LLMError(
        f"Anthropic API returned a malformed response ({type(exc).__name__})",
        recoverable=False,
    )


def _stream_error_status(exc: APIStatusError) -> int:
    """The HTTP status of a failed request, or the status matching an in-stream error event."""
    status = exc.status_code
    if status >= 400:
        return status
    body = exc.body
    error = body.get("error") if isinstance(body, dict) else None
    kind = error.get("type") if isinstance(error, dict) else None
    # An unknown in-stream error is treated as a server error.
    return STREAM_ERROR_STATUSES.get(kind, 500) if isinstance(kind, str) else 500


def _translate_error(exc: Exception) -> LLMError:
    """Map an SDK/transport exception to `LLMError` without its request or body (FND-90)."""
    if isinstance(exc, APITimeoutError | httpx2.TimeoutException):
        return LLMTimeoutError("Anthropic API call timed out")
    if isinstance(exc, APIStatusError):
        status = _stream_error_status(exc)
        return LLMError(
            f"Anthropic API returned HTTP {status}",
            recoverable=is_recoverable_status(status),
            status=status,
        )
    if isinstance(exc, APIConnectionError | httpx2.TransportError):
        return LLMError("Anthropic API connection failed", recoverable=True)
    if isinstance(exc, APIError | httpx2.HTTPError):
        # Any other SDK error (e.g. response validation): name the type only.
        return LLMError(f"Anthropic API error ({type(exc).__name__})", recoverable=False)
    # A body that is not JSON or fails SDK validation; the exception text quotes the body.
    return _malformed_response(exc)


_SDK_ERRORS = (APIError, httpx2.HTTPError, ValueError)


@dataclass(frozen=True)
class _CallParams:
    """The Messages API arguments shared by `complete` and `stream`."""

    system: list[TextBlockParam]
    tools: list[ToolParam]
    messages: list[MessageParam]
    output_config: OutputConfigParam | None

    @classmethod
    def from_request(cls, request: LLMRequest) -> "_CallParams":
        output_config: OutputConfigParam | None = None
        if request.response_schema is not None:
            output_config = {"format": {"type": "json_schema", "schema": request.response_schema}}
        return cls(
            system=_system_blocks(request.system),
            tools=_tool_params(request.tools),
            messages=_messages(request.messages),
            output_config=output_config,
        )


@dataclass
class _PendingTool:
    id: str
    name: str
    initial_input: dict[str, object]
    json_chunks: list[str] = field(default_factory=list)

    def to_call(self) -> ToolCall:
        raw = "".join(self.json_chunks)
        arguments = json.loads(raw) if raw else self.initial_input
        return ToolCall(
            id=self.id,
            name=self.name,
            arguments=dict(_require(arguments, dict, "tool_use.input")),
        )


@dataclass
class _StreamState:
    """Turns raw Messages API stream events into `LLMStreamEvent`s.

    Text deltas pass through; a `tool_use` block's `input_json_delta` fragments are joined and
    parsed when the block closes; usage (input/cache from `message_start`, cumulative output
    from `message_delta`) and the stop reason are emitted on `message_stop`.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str | None = None
    finished: bool = False
    tools: dict[int, _PendingTool] = field(default_factory=dict)

    def feed(self, event: RawMessageStreamEvent) -> list[LLMStreamEvent]:
        if event.type == "message_start":
            self._on_message_start(event)
        elif event.type == "content_block_start":
            self._on_block_start(event)
        elif event.type == "content_block_delta":
            return self._on_block_delta(event)
        elif event.type == "content_block_stop":
            return self._on_block_stop(event)
        elif event.type == "message_delta":
            self._on_message_delta(event)
        elif event.type == "message_stop":
            return self._on_message_stop()
        return []

    def _on_message_start(self, event: RawMessageStartEvent) -> None:
        usage = event.message.usage
        self.input_tokens = _require(usage.input_tokens, int, "usage.input_tokens")
        self.output_tokens = _require(usage.output_tokens, int, "usage.output_tokens")
        self.cache_read_tokens = _require(usage.cache_read_input_tokens or 0, int, "usage")
        self.cache_write_tokens = _require(usage.cache_creation_input_tokens or 0, int, "usage")

    def _on_block_start(self, event: RawContentBlockStartEvent) -> None:
        block = event.content_block
        if block.type == "tool_use":
            self.tools[event.index] = _PendingTool(
                id=_require(block.id, str, "tool_use.id"),
                name=_require(block.name, str, "tool_use.name"),
                initial_input=dict(_require(block.input or {}, dict, "tool_use.input")),
            )

    def _on_block_delta(self, event: RawContentBlockDeltaEvent) -> list[LLMStreamEvent]:
        delta = event.delta
        if delta.type == "text_delta":
            text = _require(delta.text, str, "text")
            return [LLMStreamEvent(kind="text_delta", text=text)] if text else []
        if delta.type == "input_json_delta" and event.index in self.tools:
            chunk = _require(delta.partial_json, str, "partial_json")
            self.tools[event.index].json_chunks.append(chunk)
        return []

    def _on_block_stop(self, event: RawContentBlockStopEvent) -> list[LLMStreamEvent]:
        pending = self.tools.pop(event.index, None)
        if pending is None:
            return []
        return [LLMStreamEvent(kind="tool_call", tool_call=pending.to_call())]

    def _on_message_delta(self, event: RawMessageDeltaEvent) -> None:
        self.stop_reason = event.delta.stop_reason
        totals = event.usage
        self.output_tokens = _require(totals.output_tokens, int, "usage.output_tokens")
        # Input/cache counts are cumulative here when present (server tools may add to them).
        if totals.input_tokens is not None:
            self.input_tokens = _require(totals.input_tokens, int, "usage.input_tokens")
        if totals.cache_read_input_tokens is not None:
            self.cache_read_tokens = _require(totals.cache_read_input_tokens, int, "usage")
        if totals.cache_creation_input_tokens is not None:
            self.cache_write_tokens = _require(totals.cache_creation_input_tokens, int, "usage")

    def _on_message_stop(self) -> list[LLMStreamEvent]:
        self.finished = True
        return [
            LLMStreamEvent(kind="usage", usage=self._usage()),
            LLMStreamEvent(kind="stop", stop_reason=self.stop_reason),
        ]

    def _usage(self) -> Usage:
        return Usage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
        )


class AnthropicAdapter:
    """`LLMClient` over the Anthropic Messages API."""

    def __init__(
        self,
        settings: Settings,
        model: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        key = settings.anthropic_api_key
        if key is None or not key.get_secret_value():
            raise MissingConfigError("ANTHROPIC_API_KEY")
        resolved_model = model or settings.primary_model
        if not resolved_model:
            raise MissingConfigError("ATLAS_PRIMARY_MODEL")
        self.model = resolved_model
        self._client = AsyncAnthropic(
            api_key=key.get_secret_value(),
            max_retries=0,
            timeout=settings.llm_timeout_seconds,
            http_client=http_client,
        )

    @property
    def max_retries(self) -> int:
        return self._client.max_retries

    @property
    def timeout_seconds(self) -> float | None:
        timeout = self._client.timeout
        return timeout if isinstance(timeout, float | int | None) else timeout.read

    async def complete(self, request: LLMRequest) -> LLMResponse:
        params = _CallParams.from_request(request)
        try:
            message = await self._client.messages.create(
                model=self.model,
                max_tokens=request.max_tokens,
                messages=params.messages,
                system=params.system if params.system else omit,
                tools=params.tools if params.tools else omit,
                output_config=params.output_config if params.output_config is not None else omit,
            )
        # Errors are re-raised `from None`: the SDK exception carries the HTTP request (with the
        # API key header) and the raw body, which must never reach logs (FND-90).
        except _SDK_ERRORS as exc:
            raise _translate_error(exc) from None
        try:
            return _to_response(message)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            # A 200 that parsed but is not a Message (wrong shape, null fields, bad blocks).
            raise _malformed_response(exc) from None

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Stream one Messages API call (`stream=True`) as `LLMStreamEvent`s.

        Failures before or after the first event raise `LLMError` (never swallowed); the
        fallback to `complete()` lives in `api.llm.streaming`, not here. Closing the generator
        (cancellation, client disconnect) closes the HTTP response.
        """
        params = _CallParams.from_request(request)
        state = _StreamState()
        try:
            source = await self._client.messages.create(
                model=self.model,
                max_tokens=request.max_tokens,
                messages=params.messages,
                system=params.system if params.system else omit,
                tools=params.tools if params.tools else omit,
                output_config=params.output_config if params.output_config is not None else omit,
                stream=True,
            )
            async with source:
                async for raw in source:
                    try:
                        events = state.feed(raw)
                    except (AttributeError, KeyError, TypeError, ValueError) as exc:
                        # An event of the wrong shape or tool input that is not a JSON object.
                        raise _malformed_response(exc) from None
                    for event in events:
                        yield event
        # Same as `complete`: `from None` keeps the request and body out of the traceback.
        except _SDK_ERRORS as exc:
            raise _translate_error(exc) from None
        if not state.finished:
            raise LLMError("Anthropic stream ended before message_stop", recoverable=True)
