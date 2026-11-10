"""OpenAI adapter, the secondary provider (TASK-114, DA-14, RES-05, CACHE-90, TRI-02).

Via the `openai` SDK with `max_retries=0` and `settings.llm_timeout_seconds`, model
`settings.secondary_model`, key `settings.openai_api_key`: `ToolSpec` -> `tools[type=function]`
(`parameters` = `input_schema`), `tool_calls` -> `ToolCall` (arguments parsed to dict),
`ChatMessage(role='tool')` -> `role='tool'` with `tool_call_id`, `response_schema` ->
`response_format` json_schema strict, system blocks joined as the system message, `stream()` of
text deltas / tool calls / usage, and the automatic cache read from
`usage.prompt_tokens_details.cached_tokens` (subtracted from `input_tokens`). 429/5xx/timeouts
-> recoverable `LLMError`; 400/401/403 -> non-recoverable.

This is the only module that imports the OpenAI chat SDK. It talks to Chat Completions. The SDK
(3.x) runs on `httpx2`, so an injected HTTP client must be an `httpx2.AsyncClient`. Building the
adapter never fails: the factory builds the secondary even when it is not configured, so a
missing key or model raises a non-recoverable `LLMError` on the call instead. `stop_reason` is
normalized to the Anthropic vocabulary (`end_turn`, `tool_use`, `max_tokens`, `refusal`) that the
rest of the code already reads.
"""

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx2
from openai import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    Omit,
    omit,
)
from openai.types import CompletionUsage
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionFunctionToolParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
)
from openai.types.shared_params import ResponseFormatJSONSchema

from api.config import Settings
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

PROVIDER = "openai"
SYSTEM_SEPARATOR = "\n\n"
RECOVERABLE_STATUSES = frozenset({408, 409, 429})
# `finish_reason` -> the `stop_reason` vocabulary the agent loop and the other adapters use.
FINISH_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}
# An `error` payload inside a 200 stream carries no HTTP status; map its type to the status the
# API uses for it, so `recoverable` follows the same rule as a failed request.
STREAM_ERROR_STATUSES = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
}
_SCHEMA_NAME_INVALID = re.compile(r"[^A-Za-z0-9_-]+")
_SCHEMA_NAME_MAX = 64


def is_recoverable_status(status: int) -> bool:
    """429, 408/409 and every 5xx are worth retrying."""
    return status in RECOVERABLE_STATUSES or status >= 500


def _system_message(system: list[SystemBlock]) -> list[ChatCompletionMessageParam]:
    """The non-empty system blocks joined into one `system` message (none when all are empty).

    OpenAI caches long prefixes automatically, so `cache_breakpoint` has no equivalent here.
    """
    text = SYSTEM_SEPARATOR.join(block.text for block in system if block.text)
    return [{"role": "system", "content": text}] if text else []


def _tool_params(tools: list[ToolSpec]) -> list[ChatCompletionFunctionToolParam]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


def _tool_call_param(call: ToolCall) -> ChatCompletionMessageFunctionToolCallParam:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
    }


def _message(message: ChatMessage) -> ChatCompletionMessageParam:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content,
        }
    if message.role == "assistant":
        if not message.tool_calls:
            return {"role": "assistant", "content": message.content}
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [_tool_call_param(call) for call in message.tool_calls],
        }
    return {"role": "user", "content": message.content}


def _schema_name(purpose: str) -> str:
    """A `json_schema.name` (`[A-Za-z0-9_-]{1,64}`) derived from the request purpose."""
    name = _SCHEMA_NAME_INVALID.sub("_", purpose).strip("_")[:_SCHEMA_NAME_MAX]
    return name or "response"


def _response_format(request: LLMRequest) -> ResponseFormatJSONSchema | None:
    """Strict structured output (TRI-02); the schema already arrives provider-safe."""
    if request.response_schema is None:
        return None
    return {
        "type": "json_schema",
        "json_schema": {
            "name": _schema_name(request.purpose),
            "schema": request.response_schema,
            "strict": True,
        },
    }


def _require[T](value: object, kind: type[T], field: str) -> T:
    """The SDK builds response models without validation; reject fields of the wrong type."""
    if not isinstance(value, kind) or isinstance(value, bool) and kind is int:
        raise ValueError(f"malformed response field: {field}")
    return value


def _arguments(raw: object) -> dict[str, Any]:
    """Tool arguments are a JSON object encoded as a string; an empty string means `{}`."""
    text = _require(raw, str, "tool_call.arguments")
    if not text:
        return {}
    return dict(_require(json.loads(text), dict, "tool_call.arguments"))


def _usage(usage: CompletionUsage | None) -> Usage:
    """Cached prompt tokens are reported apart from the uncached input (CACHE-90)."""
    if usage is None:
        return Usage(input_tokens=0, output_tokens=0)
    prompt = _require(usage.prompt_tokens, int, "usage.prompt_tokens")
    details = usage.prompt_tokens_details
    cached = _require(details.cached_tokens or 0, int, "usage") if details is not None else 0
    return Usage(
        input_tokens=max(prompt - cached, 0),
        output_tokens=_require(usage.completion_tokens, int, "usage.completion_tokens"),
        cache_read_tokens=cached,
        cache_write_tokens=0,
    )


def _stop_reason(finish_reason: object) -> str | None:
    if finish_reason is None:
        return None
    reason = _require(finish_reason, str, "finish_reason")
    return FINISH_REASONS.get(reason, reason)


def _to_response(completion: ChatCompletion, fallback_model: str) -> LLMResponse:
    choices = _require(completion.choices, list, "choices")
    if not choices:
        raise ValueError("malformed response field: choices")
    choice = choices[0]
    message = choice.message
    calls: list[ToolCall] = []
    for call in message.tool_calls or []:
        if call.type != "function":
            raise ValueError("malformed response field: tool_calls.type")
        calls.append(
            ToolCall(
                id=_require(call.id, str, "tool_call.id"),
                name=_require(call.function.name, str, "tool_call.name"),
                arguments=_arguments(call.function.arguments),
            )
        )
    stop_reason = _stop_reason(choice.finish_reason)
    if message.refusal:
        stop_reason = "refusal"
    model = completion.model if isinstance(completion.model, str) else fallback_model
    return LLMResponse(
        text=_require(message.content or "", str, "content"),
        tool_calls=calls,
        usage=_usage(completion.usage),
        model=model,
        provider=PROVIDER,
        stop_reason=stop_reason,
    )


def _malformed_response(exc: Exception) -> LLMError:
    return LLMError(
        f"OpenAI API returned a malformed response ({type(exc).__name__})",
        recoverable=False,
    )


def _stream_error_status(exc: APIError) -> int:
    """The status matching an in-stream `error` payload; an unknown one is a server error."""
    if exc.code == "rate_limit_exceeded":
        return 429
    return STREAM_ERROR_STATUSES.get(exc.type or "", 500)


def _translate_error(exc: Exception) -> LLMError:
    """Map an SDK/transport exception to `LLMError` without its request or body (FND-90)."""
    if isinstance(exc, APITimeoutError | httpx2.TimeoutException):
        return LLMTimeoutError("OpenAI API call timed out")
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        return LLMError(
            f"OpenAI API returned HTTP {status}",
            recoverable=is_recoverable_status(status),
            status=status,
        )
    if isinstance(exc, APIConnectionError | httpx2.TransportError):
        return LLMError("OpenAI API connection failed", recoverable=True)
    if isinstance(exc, APIResponseValidationError):
        return _malformed_response(exc)
    if isinstance(exc, APIError):
        # An `error` payload inside a 200 stream.
        status = _stream_error_status(exc)
        return LLMError(
            f"OpenAI API stream error (HTTP {status})",
            recoverable=is_recoverable_status(status),
            status=status,
        )
    if isinstance(exc, httpx2.HTTPError):
        return LLMError(f"OpenAI API error ({type(exc).__name__})", recoverable=False)
    # A body that is not JSON; the exception text quotes the body.
    return _malformed_response(exc)


_SDK_ERRORS = (APIError, httpx2.HTTPError, ValueError)
_SHAPE_ERRORS = (AttributeError, IndexError, KeyError, TypeError, ValueError)


@dataclass(frozen=True)
class _CallParams:
    """The Chat Completions arguments shared by `complete` and `stream`."""

    messages: list[ChatCompletionMessageParam]
    tools: list[ChatCompletionFunctionToolParam] | Omit
    response_format: ResponseFormatJSONSchema | Omit

    @classmethod
    def from_request(cls, request: LLMRequest) -> "_CallParams":
        response_format = _response_format(request)
        return cls(
            messages=_system_message(request.system) + [_message(m) for m in request.messages],
            tools=_tool_params(request.tools) if request.tools else omit,
            response_format=response_format if response_format is not None else omit,
        )


@dataclass
class _PendingTool:
    id: str = ""
    name: str = ""
    chunks: list[str] = field(default_factory=list)

    def to_call(self) -> ToolCall:
        return ToolCall(id=self.id, name=self.name, arguments=_arguments("".join(self.chunks)))


@dataclass
class _StreamState:
    """Turns Chat Completions chunks into `LLMStreamEvent`s.

    Text deltas pass through; tool call fragments are accumulated by `index` and emitted, in
    index order, when the choice reports its `finish_reason`; usage (the last chunk, thanks to
    `stream_options.include_usage`) and the stop reason are emitted when the stream ends.
    """

    usage: Usage | None = None
    stop_reason: str | None = None
    finished: bool = False
    tools: dict[int, _PendingTool] = field(default_factory=dict)

    def feed(self, chunk: ChatCompletionChunk) -> list[LLMStreamEvent]:
        if chunk.usage is not None:
            self.usage = _usage(chunk.usage)
        events: list[LLMStreamEvent] = []
        for choice in _require(chunk.choices, list, "choices"):
            delta = choice.delta
            if delta.content:
                text = _require(delta.content, str, "content")
                events.append(LLMStreamEvent(kind="text_delta", text=text))
            for fragment in delta.tool_calls or []:
                index = _require(fragment.index, int, "tool_call.index")
                pending = self.tools.setdefault(index, _PendingTool())
                if fragment.id:
                    pending.id = _require(fragment.id, str, "tool_call.id")
                function = fragment.function
                if function is not None:
                    if function.name:
                        pending.name = _require(function.name, str, "tool_call.name")
                    if function.arguments:
                        chunk_text = _require(function.arguments, str, "tool_call.arguments")
                        pending.chunks.append(chunk_text)
            if choice.finish_reason is not None:
                events.extend(self._finish(choice.finish_reason))
        return events

    def _finish(self, finish_reason: object) -> list[LLMStreamEvent]:
        self.finished = True
        self.stop_reason = _stop_reason(finish_reason)
        pending = [self.tools[index] for index in sorted(self.tools)]
        self.tools.clear()
        return [LLMStreamEvent(kind="tool_call", tool_call=tool.to_call()) for tool in pending]

    def closing_events(self) -> list[LLMStreamEvent]:
        return [
            LLMStreamEvent(kind="usage", usage=self.usage or Usage(0, 0)),
            LLMStreamEvent(kind="stop", stop_reason=self.stop_reason),
        ]


class OpenAIAdapter:
    """`LLMClient` over the OpenAI Chat Completions API."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.model = settings.secondary_model or ""
        key = settings.openai_api_key
        api_key = key.get_secret_value() if key is not None else ""
        self._sdk: AsyncOpenAI | None = None
        if api_key:
            self._sdk = AsyncOpenAI(
                api_key=api_key,
                max_retries=0,
                timeout=settings.llm_timeout_seconds,
                http_client=http_client,
            )

    @property
    def max_retries(self) -> int:
        return self._sdk.max_retries if self._sdk is not None else 0

    @property
    def timeout_seconds(self) -> float | None:
        if self._sdk is None:
            return self.settings.llm_timeout_seconds
        timeout = self._sdk.timeout
        return timeout if isinstance(timeout, float | int | None) else timeout.read

    def _client(self) -> AsyncOpenAI:
        """The SDK client, or a non-recoverable `LLMError` when the provider is not configured."""
        if self._sdk is None:
            raise LLMError("Missing required configuration: OPENAI_API_KEY", recoverable=False)
        if not self.model:
            raise LLMError(
                "Missing required configuration: ATLAS_SECONDARY_MODEL", recoverable=False
            )
        return self._sdk

    async def complete(self, request: LLMRequest) -> LLMResponse:
        client = self._client()
        params = _CallParams.from_request(request)
        try:
            completion = await client.chat.completions.create(
                model=self.model,
                messages=params.messages,
                max_completion_tokens=request.max_tokens,
                tools=params.tools,
                response_format=params.response_format,
            )
        # Errors are re-raised `from None`: the SDK exception carries the HTTP request (with the
        # API key header) and the raw body, which must never reach logs (FND-90).
        except _SDK_ERRORS as exc:
            raise _translate_error(exc) from None
        try:
            return _to_response(completion, self.model)
        except _SHAPE_ERRORS as exc:
            # A 200 that parsed but is not a ChatCompletion, or tool arguments that are not a
            # JSON object.
            raise _malformed_response(exc) from None

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Stream one Chat Completions call (`stream=True`) as `LLMStreamEvent`s.

        Failures before or after the first event raise `LLMError` (never swallowed); the
        fallback to `complete()` lives in `api.llm.streaming`, not here. Closing the generator
        (cancellation, client disconnect) closes the HTTP response.
        """
        client = self._client()
        params = _CallParams.from_request(request)
        state = _StreamState()
        try:
            source = await client.chat.completions.create(
                model=self.model,
                messages=params.messages,
                max_completion_tokens=request.max_tokens,
                tools=params.tools,
                response_format=params.response_format,
                stream=True,
                stream_options={"include_usage": True},
            )
            async with source:
                async for chunk in source:
                    try:
                        events = state.feed(chunk)
                    except _SHAPE_ERRORS as exc:
                        # A chunk of the wrong shape or tool arguments that are not a JSON object.
                        raise _malformed_response(exc) from None
                    for event in events:
                        yield event
        # Same as `complete`: `from None` keeps the request and body out of the traceback.
        except _SDK_ERRORS as exc:
            raise _translate_error(exc) from None
        if not state.finished:
            raise LLMError("OpenAI stream ended before finish_reason", recoverable=True)
        for event in state.closing_events():
            yield event
